"""`python -m benchbook.cli` runs the CLI, as `python -m benchbook` does. The commands live in main.py, imported
under their own name, so the sub-apps (benchbook ar) read the one STATE that the global options set."""
from .main import main

main()
