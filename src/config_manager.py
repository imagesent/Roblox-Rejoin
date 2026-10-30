import json
import os
import sys
import threading

CONFIG_FILE = "config.json"
_config_lock = threading.Lock()
REMOVED_CONFIG_KEYS = {
    "BOT_TOKEN",
    "CHANNEL_ID",
    "GUILD_ID",
    "SOCKET_HOST",
    "SOCKET_PORT",
    "SOCKET_AUTH",
}

DEFAULT_CONFIG = {
    "PLACE_ID": "PUT_PLACE_ID_HERE",
    "REJOIN_DELAY": 5,
    "CHECK_INTERVAL": 3,
    "GUI_THEME": "Chrome",
}


def get_config_path():
    """Keep configuration beside the script/executable, independent of cwd."""
    override = os.environ.get("ROBLOX_REJOINER_CONFIG")
    if override:
        return os.path.abspath(override)
    if getattr(sys, "frozen", False):
        app_dir = os.path.dirname(sys.executable)
    else:
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(app_dir, CONFIG_FILE)


def load_config():
    config_path = get_config_path()
    if not os.path.exists(config_path):
        config = DEFAULT_CONFIG.copy()
        save_config(config)
        return config

    try:
        with _config_lock, open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("configuration root must be a JSON object")
            has_removed_settings = any(key in data for key in REMOVED_CONFIG_KEYS)
            config = DEFAULT_CONFIG.copy()
            config.update(
                {
                    key: value
                    for key, value in data.items()
                    if key not in REMOVED_CONFIG_KEYS
                }
            )

        if has_removed_settings and not save_config(config):
            print("Error removing obsolete integration settings from config.json")
        return config
    except Exception as e:
        print(f"Error loading config: {e}")
        return DEFAULT_CONFIG.copy()


def save_config(config_data):
    config_path = get_config_path()
    temp_path = f"{config_path}.tmp"
    try:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with _config_lock:
            with open(temp_path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(config_data, f, indent=4)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, config_path)
        return True
    except Exception as e:
        print(f"Error saving config: {e}")
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return False
