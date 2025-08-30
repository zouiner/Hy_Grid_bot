import ujson as json
from pathlib import Path
from typing import Dict, Any


STATE_FILE = Path('state.json')


DEFAULT_STATE = {
'open_positions': {}, # symbol -> {mode, side, entry, size, stop, trail, grid_fills:[{buy,sell,filled}]}
'alerts': {}, # symbol -> {'dip': [prices], 'breakout': [prices]}
'config': {}, # overrides
}




def load_state() -> Dict[str, Any]:
if STATE_FILE.exists():
return json.loads(STATE_FILE.read_text())
return DEFAULT_STATE.copy()




def save_state(state: Dict[str, Any]):
STATE_FILE.write_text(json.dumps(state, indent=2))