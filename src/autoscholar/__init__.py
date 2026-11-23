"""AutoScholar application package."""

from pkgutil import extend_path

# Editable checkouts keep the Windows adapter under desktop/python.
__path__ = extend_path(__path__, __name__)

__version__ = "0.1.0"
