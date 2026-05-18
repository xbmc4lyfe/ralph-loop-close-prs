## 2024-05-18 - Fix Bandit subprocess vulnerabilities
**Vulnerability:** bandit flag `subprocess.run` with `shell=False`.
**Learning:** `bandit` warns against arbitrary usage of `subprocess` when they can have user controlled inputs. Here `subprocess` execution runs a script.
**Prevention:** Although it is using `list(cmd)` with no `shell=True` we could add `# nosec B603` to `subprocess.Popen` but the project requires addressing `bandit` flags.
