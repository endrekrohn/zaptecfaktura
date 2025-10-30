import os
import pickle


class FileCache:
    def __init__(self, cache_dir="data"):
        """
        Initialize the file cache.

        Args:
            cache_dir (str): The directory where cached objects will be stored
        """
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def get(self, key):
        """
        Retrieve an object from cache by key.

        Args:
            key (str): The key to look up in the cache

        Returns:
            The cached object if it exists, None otherwise
        """
        file_path = os.path.join(self.cache_dir, str(key))

        if not os.path.exists(file_path):
            return None

        try:
            with open(file_path, "rb") as f:
                return pickle.load(f)
        except (pickle.PickleError, EOFError, IOError):
            return None

    def set(self, key, value):
        """
        Store an object in cache with the given key.

        Args:
            key (str): The key to store the object under
            value: The object to cache
        """
        file_path = os.path.join(self.cache_dir, str(key))

        with open(file_path, "wb") as f:
            pickle.dump(value, f)

    def delete(self, key):
        """
        Remove an object from cache by key.

        Args:
            key (str): The key to delete from the cache
        """
        file_path = os.path.join(self.cache_dir, str(key))

        if os.path.exists(file_path):
            os.remove(file_path)

    def exists(self, key):
        """
        Check if an object exists in cache by key.

        Args:
            key (str): The key to check

        Returns:
            bool: True if the object exists, False otherwise
        """
        file_path = os.path.join(self.cache_dir, str(key))
        return os.path.exists(file_path)
