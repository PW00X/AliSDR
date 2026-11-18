# AliSDR on Arch Linux

From the AliSDR checkout, build the package with:

```sh
cd packaging/aur
makepkg -si
```

The recipe installs the tagged AliSDR source and uses Arch's Python,
NumPy and SciPy packages. For RTL-SDR hardware, install `soapysdr` and
`soapyrtlsdr`, then select the SoapySDR access method.

To update the recipe for a release, set `pkgver` to the version of the
AliSDR Git tag, run `makepkg --printsrcinfo > .SRCINFO`, and verify with
`alisdr --self-test`.
