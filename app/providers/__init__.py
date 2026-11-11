"""External providers, each behind a clean interface.

The rest of the app depends on the interfaces here (geocoding + inventory), never
on a provider's raw response shape. Swapping providers = adding a new module that
satisfies the same interface.
"""
