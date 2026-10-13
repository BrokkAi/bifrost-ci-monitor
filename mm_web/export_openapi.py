"""Write the exact OpenAPI contract consumed by the dashboard frontend."""
import json
from pathlib import Path
from mm_web.app import app


def main():
    target = Path(__file__).resolve().parents[1] / 'web' / 'openapi.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + '\n')
    print(target)


if __name__ == '__main__':
    main()
