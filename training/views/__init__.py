"""View package entrypoint.

Incremental split from the former monolithic `training/views.py` module.
Domain modules expose a clearer structure for ongoing maintenance.
"""

# Domain modules override corresponding names as they are split out.
from .auth import *  # noqa: F401,F403
from .calendar import *  # noqa: F401,F403
from .common import *  # noqa: F401,F403
from .emails import *  # noqa: F401,F403
from .people import *  # noqa: F401,F403
from .reports import *  # noqa: F401,F403
from .trainers import *  # noqa: F401,F403
from .trainings import *  # noqa: F401,F403
from .uof import *  # noqa: F401,F403
