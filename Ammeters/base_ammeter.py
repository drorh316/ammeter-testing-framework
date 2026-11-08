import socket
import time
import random
from abc import ABC, abstractmethod

NotImplementedErrorMsg = "Subclasses must implement this property."

class AmmeterEmulatorBase(ABC):
    def __init__(self, port: int):
        self.port = port
        random.seed(time.time())  # Seed the random number generator for each instance

    def start_server(self):
        """
        Starts the server to listen for client requests.
        The server will run indefinitely, handling one client request at a time.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('localhost', self.port))
            s.listen()
            print(f"{self.__class__.__name__} is running on port {self.port}")
            while True:
                conn, addr = s.accept()
                with conn:
                    print(f"Connected by {addr}")
                    data = conn.recv(1024)
                    if data == self.get_current_command:
                        # Call the specific measure_current() method defined in subclasses
                        current = self.measure_current()
                        conn.sendall(str(current).encode('utf-8'))

    @property
    @abstractmethod
    def get_current_command(self) -> bytes:
        """
        This property must be implemented by each subclass to provide the specific
        command to get the current measurement.
        """
        raise NotImplementedError(NotImplementedErrorMsg)

    @abstractmethod
    def measure_current(self) -> float:
        """
        This method must be implemented by each subclass to provide the specific
        logic for current measurement.
        """
        raise NotImplementedError(NotImplementedErrorMsg)

