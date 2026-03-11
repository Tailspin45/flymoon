import logging
import sys

# INFO shows transit detection pipeline details; change to WARNING for quiet mode
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(name="app")
