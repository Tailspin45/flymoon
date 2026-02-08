import logging
import sys

# Set to WARNING to reduce noise, or INFO for detailed logs
logging.basicConfig(stream=sys.stdout, level=logging.WARNING)
logger = logging.getLogger(name="app")
