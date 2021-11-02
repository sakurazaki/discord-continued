import json
from typing import Any, Optional, Union

try:
    import orjson
except ModuleNotFoundError:
    HAS_ORJSON = False
else:
    HAS_ORJSON = True

if HAS_ORJSON:

    def to_json(obj: Any) -> str:  # type: ignore
        return orjson.dumps(obj).decode('utf-8')

    from_json = orjson.loads  # type: ignore

else:

    def to_json(obj: Any) -> str:
        return json.dumps(obj, separators=(',', ':'), ensure_ascii=True)

    from_json = json.loads