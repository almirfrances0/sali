#!/usr/bin/env bash
# Make Secure Boot ON harmless on this machine, so the firmware can never strand you again.
#
# WHY
# ---
# This box has NO shim, runs grub-efi-amd64-unsigned, and both installed Kali kernels have an
# empty PE certificate table (unsigned). `grub-efi-amd64-signed` does not exist in Kali's archive.
# So Secure Boot ON = the firmware refuses every boot path = a trip to BIOS. That is the whole of
# problem #1. The CMOS battery (VBat 1.06V) is what keeps flipping Secure Boot back on; replacing
# it is the real fix. THIS script is the belt-and-braces half: even if the firmware resets again,
# the machine still boots.
#
# HOW
# ---
# Install Microsoft-signed shim (the MSI firmware's db trusts the Microsoft UEFI CA), then set
# shim's "validation disabled" flag. With that flag, shim loads the unsigned GRUB without checking
# it, and the kernel reads MokSBStateRT and does NOT enter lockdown — so the NVIDIA DKMS modules
# keep loading. No re-signing on kernel updates, ever.
#
# SAFETY PROPERTIES OF THIS SCRIPT
# --------------------------------
#   * Boot0002 ("kali" -> \EFI\KALI\GRUBX64.EFI) is NEVER touched. Today's boot path is unchanged.
#   * grubx64.efi and BOOTX64.EFI on the ESP are NEVER overwritten.
#   * shim-signed's postinst calls grub-install, which would rewrite your NVRAM entry. That is
#     suppressed with debconf grub2/update_nvram=false for the duration, then restored.
#   * The new shim entry is placed SECOND in BootOrder, so it is only ever reached if the first
#     entry fails — which is exactly the Secure-Boot-got-turned-on case.
#   * Every step verifies, and the script aborts on the first failure.
#
# UNDO (complete):
#   sudo efibootmgr -b <NNNN> -B                 # delete the shim entry (number printed at the end)
#   sudo efibootmgr -o 0002,0003,0004,0005,0006  # restore BootOrder
#   sudo rm -f /boot/efi/EFI/kali/shimx64.efi /boot/efi/EFI/kali/mmx64.efi
#   sudo mokutil --enable-validation             # if you already disabled it
#   sudo apt-get purge shim-signed shim-helpers-amd64-signed shim-signed-common shim-unsigned

set -euo pipefail

ESP_DISK=/dev/nvme0n1
ESP_PART=1
ESP_DIR=/boot/efi/EFI/kali
LABEL='kali (shim fallback)'

die() { echo "ABORT: $*" >&2; exit 1; }
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || die "run me with sudo"

say "0. Pre-flight — recording the state we must not damage"
efibootmgr -v > /var/tmp/efibootmgr.before.txt
ORDER_BEFORE=$(efibootmgr | awk '/^BootOrder:/{print $2}')
KALI_ENTRY=$(efibootmgr | awk '/^Boot[0-9A-F]{4}\*? kali\t/{print substr($1,5,4); exit}')
[ -n "$ORDER_BEFORE" ] || die "could not read BootOrder"
echo "BootOrder before : $ORDER_BEFORE"
echo "saved            : /var/tmp/efibootmgr.before.txt"
md5sum "$ESP_DIR/grubx64.efi" | tee /var/tmp/grubx64.md5.before

say "1. Install shim, with the postinst's NVRAM rewrite suppressed"
echo "grub-efi-amd64 grub2/update_nvram boolean false" | debconf-set-selections
restore_debconf() {
  echo "grub-efi-amd64 grub2/update_nvram boolean true" | debconf-set-selections || true
}
trap restore_debconf EXIT
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    shim-signed shim-helpers-amd64-signed mokutil
restore_debconf; trap - EXIT

say "2. Verify grub-install did NOT alter the existing boot path"
md5sum -c /var/tmp/grubx64.md5.before \
  || die "grubx64.efi CHANGED — restore it from the backup in the scratchpad before rebooting"
[ "$(efibootmgr | awk '/^BootOrder:/{print $2}')" = "$ORDER_BEFORE" ] \
  || die "BootOrder changed unexpectedly — see /var/tmp/efibootmgr.before.txt"
echo "OK: existing boot path untouched."

say "3. Verify the shim we are about to trust is genuinely signed"
[ -f /usr/lib/shim/shimx64.efi.signed ] || die "shimx64.efi.signed missing"
[ -f /usr/lib/shim/mmx64.efi.signed ]   || die "mmx64.efi.signed missing (shim-helpers not installed?)"
python3 - <<'PY' || die "shim has an EMPTY certificate table — refusing to install an unsigned shim"
import struct, sys
d = open('/usr/lib/shim/shimx64.efi.signed','rb').read()
pe = struct.unpack_from('<I', d, 0x3c)[0]                  # e_lfanew
opt = pe + 24                                              # optional header
magic = struct.unpack_from('<H', d, opt)[0]
# Certificate Table is data directory index 4; PE32+ (0x20b) puts the dirs at opt+112
base = opt + (112 if magic == 0x20b else 96)
rva, size = struct.unpack_from('<II', d, base + 4*8)
print(f"  certificate table: offset=0x{rva:x} size={size} bytes")
sys.exit(0 if size > 0 else 1)
PY

say "4. Place shim + MokManager next to the existing GRUB (adding files only)"
install -m 0644 /usr/lib/shim/shimx64.efi.signed "$ESP_DIR/shimx64.efi"
install -m 0644 /usr/lib/shim/mmx64.efi.signed   "$ESP_DIR/mmx64.efi"
ls -la "$ESP_DIR"

say "5. Create the fallback boot entry, SECOND in BootOrder"
if efibootmgr | grep -qF "$LABEL"; then
  echo "entry already exists, skipping create"
else
  efibootmgr --quiet --create --disk "$ESP_DISK" --part "$ESP_PART" \
             --loader '\EFI\kali\shimx64.efi' --label "$LABEL"
fi
NEW=$(efibootmgr | awk -v l="$LABEL" 'index($0,l){print substr($1,5,4); exit}')
[ -n "$NEW" ] || die "could not find the new entry"
# efibootmgr puts new entries first; rebuild as: real kali entry, then shim, then the rest.
REST=$(echo "$ORDER_BEFORE" | tr ',' '\n' | grep -vx "$KALI_ENTRY" | grep -vx "$NEW" | paste -sd,)
efibootmgr --quiet -o "${KALI_ENTRY},${NEW},${REST}"
echo "BootOrder now    : $(efibootmgr | awk '/^BootOrder:/{print $2}')"
echo "shim entry       : Boot$NEW  ($LABEL)"

say "6. Request 'Secure Boot validation disabled' in shim"
echo "You will be asked for a NEW one-time password. Choose something short you can retype"
echo "blind at a blue console screen in a moment — e.g. salishim"
mokutil --disable-validation

cat <<EOF

================================================================================
DONE — but one step is left, and it needs you AT THE KEYBOARD.

The request above is pending. It is only processed when shim actually runs, and
your normal boot entry goes straight to GRUB. So, when you are sitting at the
machine and ready for one reboot:

    sudo efibootmgr --bootnext $NEW && sudo reboot

On the next boot you will get a blue "Shim UEFI key management" screen:
    -> Change Secure Boot state
    -> it asks for characters of the password you just set (e.g. "character 3")
    -> Yes / OK, then Reboot

Verify afterwards:
    mokutil --sb-state          # SecureBoot disabled
    dmesg | grep -i lockdown    # nothing = good, the kernel is not locked down
    lsmod | grep nvidia         # modules still loaded

After that, if the firmware ever flips Secure Boot back on, entry Boot$KALI_ENTRY
fails, the firmware falls through to Boot$NEW, shim boots GRUB without verifying
it, and you land in Kali with the NVIDIA driver working. No BIOS trip.

If anything looks wrong at any point, press F11 at the MSI splash and pick the
plain "kali" entry — that path is byte-for-byte what you boot today.
================================================================================
EOF
