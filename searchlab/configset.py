"""The lab configset: _default with merge/indexing knobs parameterized.

Solr's Config API can't edit indexConfig (merge policy, RAM buffer) — those
aren't on the editable whitelist. But solrconfig.xml substitutes
${user.property} references, and `set-user-property` writes those live with
an automatic core reload. So the lab installs its own configset once per
cluster: a copy of _default pulled from the running container (so it always
matches the Solr version), with indexConfig rewritten to reference
${searchlab.*} properties.

Collections created from this configset get the merge knobs in the control
panel; everything else behaves exactly like _default. Any failure falls
back to plain _default with a warning — creating collections must never
break because of tuning.
"""

from __future__ import annotations

import subprocess
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

import httpx

CONFIGSET_NAME = "searchlab"

INDEX_CONFIG_BLOCK = """\
    <ramBufferSizeMB>${searchlab.ramBufferMB:100}</ramBufferSizeMB>
    <mergePolicyFactory class="org.apache.solr.index.TieredMergePolicyFactory">
      <int name="segmentsPerTier">${searchlab.segmentsPerTier:10}</int>
      <double name="maxMergedSegmentMB">${searchlab.maxMergedSegMB:5120}</double>
      <double name="deletesPctAllowed">${searchlab.deletesPctAllowed:33}</double>
    </mergePolicyFactory>
"""


def patch_solrconfig(xml: str) -> str:
    """Insert the parameterized indexConfig block into a solrconfig.xml."""
    if "searchlab.ramBufferMB" in xml:
        return xml  # already patched
    if "</indexConfig>" in xml:
        return xml.replace("</indexConfig>",
                           INDEX_CONFIG_BLOCK + "  </indexConfig>", 1)
    # no indexConfig section at all: add one before the closing tag
    block = "  <indexConfig>\n" + INDEX_CONFIG_BLOCK + "  </indexConfig>\n</config>"
    return xml.replace("</config>", block, 1)


def _zip_dir(root: Path) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(root))
    return buf.getvalue()


def ensure_lab_configset(spec, timeout: float = 30.0) -> bool:
    """Install the lab configset if missing. True if it's available to use."""
    base = spec.base_url()  # http://localhost:8983/solr
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(f"{base}/admin/configs",
                           params={"action": "LIST", "wt": "json"})
            r.raise_for_status()
            if CONFIGSET_NAME in r.json().get("configSets", []):
                return True

            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "configset"
                cp = subprocess.run(
                    ["docker", "cp",
                     f"{spec.project_name}-solr1:/opt/solr/server/solr/configsets/_default/conf",
                     str(dest)],
                    capture_output=True, text=True)
                if cp.returncode != 0:
                    print(f"searchlab: could not copy _default configset "
                          f"({cp.stderr.strip()}); merge knobs disabled")
                    return False
                solrconfig = dest / "solrconfig.xml"
                solrconfig.write_text(patch_solrconfig(solrconfig.read_text()))
                payload = _zip_dir(dest)

            r = client.post(f"{base}/admin/configs",
                            params={"action": "UPLOAD", "name": CONFIGSET_NAME,
                                    "wt": "json"},
                            content=payload,
                            headers={"Content-Type": "application/octet-stream"})
            r.raise_for_status()
            return True
    except (httpx.HTTPError, OSError) as e:
        print(f"searchlab: lab configset unavailable ({e}); merge knobs disabled")
        return False
