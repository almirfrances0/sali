---
name: Kali Linux
tags:
  - kali
  - linux
  - systemctl
  - apt
  - shell
  - nmap
  - security
---

# Kali Linux

This machine is Sali's home — a Kali Linux box. Move around it freely, but keep task-generated files inside the task workspace.

## Packages & services
- Install: `sudo apt update && sudo apt install -y <pkg>`. Verify it landed (`which <bin>` / `dpkg -l <pkg>`) before marking a step done.
- Services: `systemctl status/start/stop <unit>`. A control action's evidence is the resulting `is-active` state, not the command's exit code alone.

## Shell
- Prefer absolute paths for task files; a `cd` inside a command is transient and does not change the task's authoritative workspace.
- Quote paths with spaces; avoid destructive globs (`rm -rf`) unless explicitly asked and the target is verified.

## Security tooling
- Kali ships pentest tools (nmap, sqlmap, metasploit, …). Use them only for authorized testing on targets you're permitted to touch.

## Verification
- After any system change, re-observe reality (package present, service active, port listening) rather than trusting the command output.
