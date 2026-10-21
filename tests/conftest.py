import os

# Never let a test open a real window: wheels and toasts are always-on-top. CI sets this too.
# Tests that need the real window system (test_real_dnd) start their own process without it.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
