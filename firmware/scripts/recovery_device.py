"""Device guard compatible with the legacy recovery updater binary."""

# Stock recovery's /default.prop was extracted from this phone's verified image.
# Reading it directly avoids the legacy updater's failing live-property lookup.
DEVICE_CHECK = '''ui_print(concat("Recovery device: ", file_getprop("/default.prop", "ro.product.device")));
assert(file_getprop("/default.prop", "ro.product.device") == "Nokia 8110 4G");
'''
