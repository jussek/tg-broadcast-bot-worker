
import importlib
import os
import os.path
import site
import sys
import time

# Cold start baseline, read back by vercel_runtime.vc_init
_vc_boot_start_ms = int(time.monotonic() * 1000)

_here = os.path.dirname(__file__)

os.environ.update({
  "__VC_PY_BOOT_START_MS": str(_vc_boot_start_ms),
  "__VC_HANDLER_MODULE_NAME": "api.index",
  "__VC_HANDLER_ENTRYPOINT": "api/index.py",
  "__VC_HANDLER_ENTRYPOINT_ABS": os.path.join(_here, "api/index.py"),
  "__VC_HANDLER_VENDOR_DIR": "_vendor",
  "__VC_HANDLER_VARIABLE_NAME": "app"
})

_vendor_rel = '_vendor'
_vendor = os.path.normpath(os.path.join(_here, _vendor_rel))

if os.path.isdir(_vendor):
    # Process .pth files like a real site-packages dir
    site.addsitedir(_vendor)

    # Move _vendor to the front (after script dir if present)
    try:
        while _vendor in sys.path:
            sys.path.remove(_vendor)
    except ValueError:
        pass

    # Put vendored deps ahead of site-packages but after the script dir
    idx = 1 if (sys.path and sys.path[0] in ('', _here)) else 0
    sys.path.insert(idx, _vendor)

    importlib.invalidate_caches()

from vercel_runtime.vc_init import vc_handler
