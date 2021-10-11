import sys
from pathlib import Path
import json
file = Path(sys.argv[1])

data = json.loads(file.read_text())
data['homepage'] = sys.argv[2]
file.write_text(json.dumps(data))
