import argparse
import warnings
from collections import OrderedDict
from utils.num_nodes_grouped_natural_id_partitioner import NumNodesGroupedNaturalIdPartitioner
import pickle

import flwr as fl
from flwr_datasets import FederatedDataset
from flwr_datasets.partitioner.iid_partitioner import IidPartitioner
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torchvision.transforms import Compose, Normalize, ToTensor
from tqdm import tqdm
import socket
import numpy as np
from models.ResNet import ResNet18
from models.simpleCNN import SimpleCNN

from utils.serializers import sparse_parameters_to_ndarrays

# #############################################################################
# 1. Regular PyTorch pipeline: nn.Module, train, test, and DataLoader
# #############################################################################

warnings.filterwarnings("ignore", category=UserWarning)
DEVICE = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


def train(net, trainloader, epochs):
    """Train the model on the training set."""
    criterion = torch.nn.CrossEntropyLoss()
    optimizer = torch.optim.SGD(net.parameters(), lr=0.001, momentum=0.9)
    for _ in range(epochs):
        for batch in tqdm(trainloader, "Training"):
            images = batch["img"]
            labels = batch["label"]
            optimizer.zero_grad()
            criterion(net(images.to(DEVICE)), labels.to(DEVICE)).backward()
            optimizer.step()


def test(net, testloader):
    """Validate the model on the test set."""
    criterion = torch.nn.CrossEntropyLoss()
    correct, loss = 0, 0.0
    with torch.no_grad():
        for batch in tqdm(testloader, "Testing"):
            images = batch["img"].to(DEVICE)
            labels = batch["label"].to(DEVICE)
            outputs = net(images)
            loss += criterion(outputs, labels).item()
            correct += (torch.max(outputs.data, 1)[1] == labels).sum().item()
    accuracy = correct / len(testloader.dataset)
    return loss, accuracy


def agg(param_list, len_datasets):
    final_params = []
    for i in range(len(param_list[0])):
        final_params.append(np.mean(np.array([param_list[j][i] for j in range(len(param_list))]), axis=0))

    return final_params

def load_data(node_id):
    """Load partition CIFAR10 data."""
    #part = NumNodesGroupedNaturalIdPartitioner("label",3,3)
    part = IidPartitioner(3)
    fds = FederatedDataset(dataset="cifar10", partitioners={"train": part})
    partition = fds.load_partition(node_id)
    # Divide data on each node: 80% train, 20% test
    partition_train_test = partition.train_test_split(test_size=0.2)
    pytorch_transforms = Compose(
        [ToTensor(), Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))]
    )

    def apply_transforms(batch):
        """Apply transforms to the partition from FederatedDataset."""
        batch["img"] = [pytorch_transforms(img) for img in batch["img"]]
        return batch

    partition_train_test = partition_train_test.with_transform(apply_transforms)
    trainloader = DataLoader(partition_train_test["train"], batch_size=32, shuffle=True)
    testloader = DataLoader(partition_train_test["test"], batch_size=32)
    return trainloader, testloader


# #############################################################################
# 2. Federation of the pipeline with Flower
# #############################################################################

# Get node id
parser = argparse.ArgumentParser(description="Flower")
parser.add_argument(
    "--node-id",
    choices=[0, 1, 2, 3],
    required=True,
    type=int,
    help="Partition of the dataset divided into 3 iid partitions created artificially.",
)

parser.add_argument(
    "--num_clients",
    required=True,
    type=int,
    help="Number of clients this node has",
)

parser.add_argument(
    "--port",
    required=True,
    type=int,
    help="Port to expose for this client",
)

args = parser.parse_args()

node_id = args.node_id
num_clients = args.num_clients
port = args.port


# Load model and data (simple CNN, CIFAR-10)
net = SimpleCNN().to(DEVICE)
trainloader, testloader = load_data(node_id=node_id)


# Define Flower client
class FlowerClient(fl.client.NumPyClient):
    def __init__(self, num_clients, port):
        self.num_clients = num_clients
        self.serversocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.serversocket.bind((socket.gethostname(), port))
        self.serversocket.listen(self.num_clients)

    def get_parameters(self, config):
        return [val.cpu().numpy() for _, val in net.state_dict().items()]

    def set_parameters(self, parameters):
        params_dict = zip(net.state_dict().keys(), parameters)
        state_dict = OrderedDict({k: torch.tensor(v) for k, v in params_dict})
        net.load_state_dict(state_dict, strict=True)

    def fit(self, parameters, config):
        self.set_parameters(parameters)
        #Train on params
        train(net, trainloader, epochs=1)
        #Become a server and receive params from children
        recv_params = []
        len_datasets = [] 
        for i in range(self.num_clients):

            (conn, addr) = self.serversocket.accept()
            data = []
            while True:
                packet = conn.recv(4096)
                packet_len = len(packet)
                data.append(packet)
                #if packet_len < 4096:
                try:
                    pickle_data = b"".join(data)
                    data_arr = pickle.loads(pickle_data)
                    break
                except pickle.UnpicklingError:
                    continue


            conn.shutdown(socket.SHUT_RDWR)

            print(f"Len received: {len(pickle_data)}")

            data_arr = pickle.loads(pickle_data)
            recv_params.append(sparse_parameters_to_ndarrays(data_arr[0]))
            len_datasets.append(data_arr[1])

            conn.close()



        

        recv_params.append(self.get_parameters(config={}))
        len_datasets.append(len(trainloader.dataset))
        #Aggregate parameters
        new_params = agg(recv_params, len_datasets)
        #Return aggregated parameters
        self.set_parameters(new_params)
        len_data = sum(len_datasets)
   
        # len(trainloader.dataset) has to be the sum of the previous len (Check comment in client)
        return self.get_parameters(config={}), len_data, {}

    def evaluate(self, parameters, config):
        self.set_parameters(parameters)
        loss, accuracy = test(net, testloader)
        return loss, len(testloader.dataset), {"accuracy": accuracy, "loss": loss}


# Start Flower client
fl.client.start_client(
    server_address="127.0.0.1:8080",
    client=FlowerClient(num_clients, port).to_client(),
)
