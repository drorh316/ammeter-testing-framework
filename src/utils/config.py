import yaml
from typing import Dict

def load_config(config_path: str) -> Dict:
    """
    טעינת קובץ הקונפיגורציה
    """
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)
