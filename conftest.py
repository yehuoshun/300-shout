import os
import sys

# 让 pytest 能从仓库根导入 capture/detect/sender/main/calibrate
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
