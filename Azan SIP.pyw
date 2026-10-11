# Double-click to start Azan SIP. It runs in the background and opens its control panel.
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

from azan_sip.web import main  # noqa: E402

main()
