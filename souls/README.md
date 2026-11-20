# Souls

One file per role that plans, builds or decides: `pm.md`, `dev.md`, `devops.md`, `delivery.md`. A project can
override one with `souls/<project>/<role>.md`. Each is short (under ~1,500 characters) and is added to that role's
system prompt on every run, after its skill (`orgrove/memory.py`).

**People write these, agents never do.** Change them by PR. They say who the role is and how this owner wants things
done; project facts belong in the role's project memory, and mistakes in `lessons/`.
