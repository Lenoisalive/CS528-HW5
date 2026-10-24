"""Read-only checks used by deploy.sh before reusing existing cloud resources."""

import json
import sys


def require(condition, message):
    if not condition:
        raise SystemExit(f"Existing resource conflicts with deployment: {message}")


kind, filename, *args = sys.argv[1:]
with open(filename) as handle:
    data = json.load(handle)

if kind == "role":
    require(not data.get("deleted") and data.get("stage") != "DISABLED", "role is disabled/deleted")
    require(set(data.get("includedPermissions", [])) == set(args[0].split(",")),
            "custom role permissions differ; review the role instead of overwriting it")
elif kind == "subscription":
    project, topic = args
    require(data.get("topic") == f"projects/{project}/topics/{topic}", "subscription topic differs")
    require(not data.get("pushConfig", {}).get("pushEndpoint"), "subscription is configured for push")
    require(not data.get("bigqueryConfig") and not data.get("cloudStorageConfig"),
            "subscription is an export subscription")
    require(not data.get("filter"), "subscription filter may discard required messages")
    require(not data.get("detached", False), "subscription is detached")
elif kind == "address":
    region = args[0]
    require(data.get("region", "").endswith(f"/regions/{region}"), "static IP region differs")
    require(data.get("addressType") == "EXTERNAL", "address is not external")
    require(data.get("ipVersion", "IPV4") == "IPV4", "address is not IPv4")
    require(data.get("networkTier", "PREMIUM") == "PREMIUM", "address is not PREMIUM tier")
elif kind == "firewall":
    project, network, tag, sources = args
    require(data.get("network", "").endswith(f"/projects/{project}/global/networks/{network}"),
            "firewall network differs")
    require(data.get("direction") == "INGRESS" and not data.get("disabled", False),
            "firewall is disabled or not ingress")
    require(data.get("priority", 1000) == 1000, "firewall priority differs")
    require(data.get("targetTags") == [tag], "firewall target tags differ")
    require(set(data.get("sourceRanges", [])) == set(sources.split(",")), "firewall source ranges differ")
    require(not data.get("sourceTags") and not data.get("sourceServiceAccounts")
            and not data.get("targetServiceAccounts") and not data.get("denied"),
            "firewall has additional selectors or deny rules")
    require(data.get("allowed") == [{"IPProtocol": "tcp", "ports": ["8080"]}],
            "firewall must allow only tcp:8080")
elif kind == "vm":
    project, network, account, machine, address, tag = args
    require(data.get("status") in ("RUNNING", "TERMINATED"), "VM is in a transitional/suspended state")
    require(data.get("machineType", "").endswith(f"/machineTypes/{machine}"), "VM machine type differs")
    accounts = data.get("serviceAccounts", [])
    require(len(accounts) == 1 and accounts[0].get("email") == account, "VM service account differs")
    require("https://www.googleapis.com/auth/cloud-platform" in accounts[0].get("scopes", []),
            "VM lacks cloud-platform scope")
    nics = data.get("networkInterfaces", [])
    require(len(nics) == 1 and nics[0].get("network", "").endswith(
        f"/projects/{project}/global/networks/{network}"), "VM network differs")
    access = nics[0].get("accessConfigs", [])
    require(len(access) == 1, "VM must have one external access configuration for outbound access")
    if address != "-":
        require(access[0].get("natIP") == address, "VM does not use the reserved static IP")
    tags = data.get("tags", {}).get("items", [])
    if tag != "-":
        require(tag in tags, "VM lacks the web firewall tag")
    boot = [disk for disk in data.get("disks", []) if disk.get("boot")]
    require(len(boot) == 1 and any(license.endswith("/projects/debian-cloud/global/licenses/debian-12-bookworm")
            for license in boot[0].get("licenses", [])), "startup scripts require a Debian 12 boot disk")
else:
    raise SystemExit(f"Unknown resource kind: {kind}")
