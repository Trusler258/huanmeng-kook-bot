import inspect
import khl
from khl import Client
print("khl file:", khl.__file__)
print("--- Client.send ---")
print(inspect.getsource(Client.send))
print("--- Client.create_asset ---")
try:
    print(inspect.getsource(Client.create_asset))
except Exception as e:
    print("create_asset:", e)