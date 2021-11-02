import json
from typing import Any, Optional, Union
import datetime

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

def _parse_ratelimit_header(request, *, use_clock=False):
    reset_after = request.headers.get('X-Ratelimit-Reset-After')
    if use_clock or not reset_after:
        utc = datetime.timezone.utc
        now = datetime.datetime.now(utc)
        reset = datetime.datetime.fromtimestamp(float(request.headers['X-Ratelimit-Reset']), utc)
        return (reset - now).total_seconds()
    else:
        return float(reset_after)