"""Platform-appropriate local data directory. No network access, no prompts."""
import os
import sys
from pathlib import Path

ENV_OVERRIDE = 'CONTEXT_HUB_DATA_DIR'


def default_data_dir():
    """Per-OS default; override with the CONTEXT_HUB_DATA_DIR environment variable."""
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == 'darwin':
        return home / 'Library' / 'Application Support' / 'ContextHub'
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA')
        return (Path(base) if base else home / 'AppData' / 'Local') / 'ContextHub'
    xdg = os.environ.get('XDG_DATA_HOME')
    base = Path(xdg).expanduser() if xdg else home / '.local' / 'share'
    return base / 'context-hub'
