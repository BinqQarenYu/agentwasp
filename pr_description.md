🔒 Fix Path Traversal Bypass in Sandbox _restricted_open

🎯 **What:** The vulnerability fixed
Stateful objects returning different values in `__str__` and `__fspath__` could bypass sandbox path restrictions. Furthermore, when `os.path.realpath` raises an exception, the fallback `str(path)` was insecure because it leaves potential `..` traversal components unresolved.

⚠️ **Risk:** The potential impact if left unfixed
Malicious code executed within the sandbox could break out and read or write files anywhere on the host filesystem that the executing user has access to.

🛡️ **Solution:** How the fix addresses the vulnerability
The fix securely grabs the string representation using `os.fsdecode(path)` which properly handles bytes, string, and path-like objects (preventing stateful `__str__` / `__fspath__` mismatches). If `os.path.realpath` fails, it falls back to `os.path.abspath` to securely resolve any `..` traversal components before verifying the resulting path against the sandbox directory.
