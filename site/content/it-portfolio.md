---
title: "IT Portfolio"
layout: "it-portfolio"
intro: |
  I've been running a self-hosted home network since 2024 — not as a
  sandbox, but as infrastructure I actually depend on. Everything from
  DNS and VPN access to media, backups, and game servers runs on it.
  Designing, breaking, and fixing it has been the most useful technical
  education I've had.

  The network is segmented into isolated zones — internal services,
  IoT, home devices, and a public-facing DMZ — enforced by a dedicated
  firewall with a default-deny policy between segments. The compute
  layer is a three-node virtualisation cluster. All application
  deployments are managed through Git and Docker Compose; no
  click-ops, full rollback capability.

  What I find genuinely interesting about it: real infrastructure has
  real failure modes. When something breaks I have to dig into logs,
  trace routing decisions, and work out what I missed. That feedback
  loop is hard to replicate any other way.
---

```
                         ┌──────────────────────────────┐
                         │           INTERNET           │
                         └──────────────┬───────────────┘
                                        │
                         ┌──────────────▼───────────────┐
                         │       OPNsense Firewall       │
                         │   L3 core · default-deny      │
                         └──┬─────┬──────┬──────┬────────┘
                            │     │      │      │
              ┌─────────────▼┐  ┌─▼───┐ ┌▼────┐ ┌▼─────────────────┐
              │  Management  │  │ IoT │ │ Home│ │ DMZ (isolated)   │
              │  + Core Infra│  │     │ │     │ │ Minecraft server │
              └──────┬───────┘  └──┬──┘ └─────┘ │ osu! server      │
                     │             │ DNS only    │ Public services  │
              ┌──────▼─────────────────────────┐ └──────────────────┘
              │        Lab / Services          │
              │                                │
              │  3-node Proxmox cluster        │
              │  ~25 containerised services    │
              │                                │
              │  · Identity & SSO              │
              │  · Media & photo library       │
              │  · Monitoring & alerting       │
              │  · DNS filtering & VPN         │
              │  · Automated backups           │
              └────────────────────────────────┘
```

The DMZ runs on a physically separate node — VLAN 50 is pruned at
the switch so it's only reachable from that node. Public services
are fronted by Cloudflare; the internal network is never directly
exposed.

Hosting game servers in the DMZ is something I've particularly
enjoyed — it forced me to think carefully about how you give
something internet access while keeping it genuinely isolated
from everything else on the network.

---

## Research

### Lightweight Cryptography for Secure Firmware Updates in IoT
**Undergraduate Thesis — Glasgow Caledonian University, 2025**
Supervisor: Dr. Rajiv Singh

Evaluated AES-128, ChaCha20, ECDSA, and Ed25519 for securing
over-the-air firmware updates in resource-constrained IoT devices.
Simulated attack scenarios using mitmproxy, analysed traffic with
Wireshark and tcpdump, and measured performance trade-offs on
low-power hardware. The question was whether you could get adequate
security without the compute overhead that makes standard approaches
impractical on constrained devices.

---

## Projects

### Dijhitech Research Labs — Internal Penetration Test
**Applied Penetration Testing — Graded Group Project**

Black-box pentest in a simulated enterprise environment. Identified
and documented nine vulnerabilities with remediation recommendations.
Led the team, handled task delegation, and wrote the final report.
