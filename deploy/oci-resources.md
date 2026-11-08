# OCI resources: obsidian-vps

- Created: 2026-11-08
- Region: eu-madrid-1
- Availability domain: omae:EU-MADRID-1-AD-1
- Compartment (root/tenancy): ocid1.tenancy.oc1..aaaaaaaaxwa42bcxewkexuzdbfmitrnhrsvhr6gcizujjgprksblpotjns3q

## Network (created 2026-11-08)

| Resource | Name | OCID |
|---|---|---|
| VCN (10.0.0.0/16, dns obsvcn) | obsidian-vcn | ocid1.vcn.oc1.eu-madrid-1.amaaaaaa6zsazqiat45rw43e6tan7kmh3unx2pyfba4avffnifwbhxaqpjma |
| Internet gateway | obsidian-igw | ocid1.internetgateway.oc1.eu-madrid-1.aaaaaaaanhcmphcqirkyegxzxpmpgrelkg2im3v5gypbsjaejthk4tlfnlvq |
| Route table (0.0.0.0/0 -> IGW) | obsidian-rt | ocid1.routetable.oc1.eu-madrid-1.aaaaaaaa76yzlzibtzicgwyvdiutrfd4ayyrhyc4wap52voix6nnnsfoaipa |
| Security list | obsidian-sl | ocid1.securitylist.oc1.eu-madrid-1.aaaaaaaacgkrsuoknhdjcs6zknk2ytfwxokbgbthhyuvbswkvtpybz5z7m4q |
| Public regional subnet (10.0.0.0/24, dns pub) | obsidian-pub | ocid1.subnet.oc1.eu-madrid-1.aaaaaaaarc4xr7m22fk5anok43uyqwshzqqrw5d3llkfywprqjoivhq2v3ja |

The subnet uses only obsidian-rt and obsidian-sl. The VCN's auto-created default route table and security list exist but are not attached to the subnet.

Security list obsidian-sl (all rules stateful):
- Egress: all protocols to 0.0.0.0/0
- Ingress TCP 80 from 0.0.0.0/0
- Ingress TCP 443 from 0.0.0.0/0
- Ingress TCP 22 from 79.117.186.252/32 only
- Ingress ICMP type 3 code 4 from 0.0.0.0/0 (path MTU discovery)

## Image

- Canonical-Ubuntu-24.04-aarch64-2026.10.27-0
- ocid1.image.oc1.eu-madrid-1.aaaaaaaal7jmbfkizhtwk56chfo5afzif33a2wig36bk6h33jalamck3lhdq

## Instance

| Field | Value |
|---|---|
| Name | obsidian-vps |
| OCID | ocid1.instance.oc1.eu-madrid-1.anwwcljr6zsazqicptkknnv422fqg23jejb33unfqxj4ymvycmcwaysb3owa |
| Shape | VM.Standard.A1.Flex, 1 OCPU / 6 GB (Always Free) |
| Fault domain | FAULT-DOMAIN-3 (chosen by OCI) |
| Boot volume (50 GB) | ocid1.bootvolume.oc1.eu-madrid-1.abwwcljrioysl2vn2jwjg44g3hcl4tzm23qj7yw5qqlsxtn3ppqdiau5jhqa |
| Public IP (ephemeral) | 51.170.63.47 |
| Created | 2026-11-08 20:40 UTC, after ~1.5 h of retries on `Out of host capacity` |

DNS: `obsidian.mcp.htarrega.me` A 51.170.63.47 (Cloudflare, DNS only / not proxied).

## Notifications

| Resource | Name | OCID |
|---|---|---|
| ONS topic (email subscription to the owner) | obsidian-alerts | ocid1.onstopic.oc1.eu-madrid-1.amaaaaaa6zsazqiafp7bpi5yvrd2liucznw6lhuewcubek36xl4rda4mpkuq |

## Budget

- `General`: 1 USD-equivalent / month on the root compartment, alert rule on actual spend.

## Notes

- The ephemeral public IP changes if the instance is stopped/terminated and recreated; update the DNS record then (or reserve a public IP, free up to 1 reserved IP on Always Free).
- Oracle may reclaim idle Always Free instances (7-day CPU, network and memory p95 below 20%) unless the tenancy is Pay As You Go.
