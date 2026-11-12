"""Gece yükleme işi. Ayrıntılar: src/izmir_mcp/ingest.py

    python jobs/ingest_static.py [--dry-run] [--only districts stations ...]
"""

import sys

from izmir_mcp.ingest import main

if __name__ == "__main__":
    sys.exit(main())
