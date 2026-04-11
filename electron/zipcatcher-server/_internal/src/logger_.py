import logging
import sys

# WARNING = quiet terminal; INFO = transit pipeline details; DEBUG = verbose
logging.basicConfig(stream=sys.stdout, level=logging.WARNING)
logger = logging.getLogger(name="app")
