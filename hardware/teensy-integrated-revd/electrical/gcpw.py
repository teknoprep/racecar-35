#!/usr/bin/env python3
"""GNSS grounded-coplanar-waveguide geometry for the Rev D stackup.

Uses the standard CPW-with-lower-ground-plane formulation (Ghione & Naldi;
also the form used by Simons, "Coplanar Waveguide Circuits, Components and
Systems"), evaluated with an AGM complete elliptic integral.

    a  = W/2                     half strip width
    b  = W/2 + S                 half width to the coplanar ground edge
    k0 = a/b
    k1 = sinh(pi*a/2h) / sinh(pi*b/2h)      h = dielectric thickness to the plane
    eps_eff = 1 + (er-1)/2 * [K(k1)/K(k1')] * [K(k0)/K(k0')]
    Z0      = (30*pi / sqrt(eps_eff)) * K(k0')/K(k0)

Sanity limit: as h -> infinity, k1 -> k0 and eps_eff -> (er+1)/2, which is the
classic infinite-substrate CPW result. That limit is asserted below.
"""
import math

C0 = 299_792_458.0


def K(k):
    """Complete elliptic integral of the first kind, AGM method."""
    kp = math.sqrt(1.0 - k * k)
    a, b = 1.0, kp
    for _ in range(60):
        a, b = 0.5 * (a + b), math.sqrt(a * b)
        if abs(a - b) < 1e-16:
            break
    return math.pi / (2.0 * a)


def z0_gcpw(W, S, H, er):
    a, b = W / 2.0, W / 2.0 + S
    k0 = a / b
    k0p = math.sqrt(1.0 - k0 * k0)
    k1 = math.sinh(math.pi * a / (2.0 * H)) / math.sinh(math.pi * b / (2.0 * H))
    k1p = math.sqrt(1.0 - k1 * k1)
    q = (K(k1) / K(k1p)) / (K(k0) / K(k0p))
    eeff = 1.0 + (er - 1.0) / 2.0 * q
    return (30.0 * math.pi / math.sqrt(eeff)) * K(k0p) / K(k0), eeff


def solve_W(S, H, er, target=50.0, lo=0.05, hi=3.0):
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        z, _ = z0_gcpw(mid, S, H, er)
        if z > target:      # wider strip -> lower impedance
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


if __name__ == '__main__':
    print('=== validation: infinite substrate must collapse to classic CPW ===')
    W, S, er = 0.80, 0.15, 4.4
    z_inf, e_inf = z0_gcpw(W, S, 1e6, er)
    a, b = W / 2, W / 2 + S
    k = a / b
    classic = 30 * math.pi / math.sqrt((er + 1) / 2) * K(math.sqrt(1 - k * k)) / K(k)
    print(f'  h -> infinity : {z_inf:6.2f} ohm   eps_eff {e_inf:.4f}')
    print(f'  classic CPW   : {classic:6.2f} ohm   eps_eff {(er+1)/2:.4f}')
    print(f'  delta         : {abs(z_inf-classic):.4f} ohm')

    print()
    print('=== Rev D stackup candidates (PCBWay 4-layer, 1.6 mm) ===')
    er = 4.4
    print('  gap S = 0.15 mm (current design rule)')
    print('  H (prepreg to In1.Cu)   W for 50 ohm    Z0 at W=0.80')
    for H in (0.10, 0.15, 0.20, 0.25, 0.36):
        w50 = solve_W(0.15, H, er)
        z_now, _ = z0_gcpw(0.80, 0.15, H, er)
        print(f'      {H:.2f} mm              {w50:.3f} mm        {z_now:5.1f} ohm')

    print()
    print('=== sensitivity to er at H = 0.20 mm, S = 0.15 mm ===')
    for er_i in (4.2, 4.4, 4.6, 4.8):
        w50 = solve_W(0.15, 0.20, er_i)
        print(f'   er = {er_i}:  W = {w50:.3f} mm')
