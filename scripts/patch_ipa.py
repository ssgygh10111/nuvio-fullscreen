name: Patch latest Nuvio Enhanced

on:
  schedule:
    - cron: "17 */6 * * *"
  workflow_dispatch: {}

permissions:
  contents: write

env:
  UPSTREAM: luqmanfadlli/NuvioMobile-Enhanced
  OUT_NAME: Nuvio-Enhanced-fullscreen.ipa

jobs:
  patch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Find latest upstream release with an IPA
        id: up
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh api "repos/$UPSTREAM/releases?per_page=20" > releases.json
          python3 - <<'PY' >> "$GITHUB_OUTPUT"
          import json
          for r in json.load(open("releases.json")):
              if r.get("draft"):
                  continue
              ipa = next((a for a in r["assets"] if a["name"].lower().endswith(".ipa")), None)
              if ipa:
                  print(f"tag={r['tag_name']}")
                  print(f"url={ipa['browser_download_url']}")
                  print(f"file={ipa['name']}")
                  break
          PY

      - name: Skip if already done
        id: check
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          if [ -z "${{ steps.up.outputs.tag }}" ]; then echo "skip=true" >> "$GITHUB_OUTPUT"; exit 0; fi
          if gh release view "${{ steps.up.outputs.tag }}" -R "$GITHUB_REPOSITORY" >/dev/null 2>&1; then
            echo "skip=true" >> "$GITHUB_OUTPUT"
          else
            echo "skip=false" >> "$GITHUB_OUTPUT"
          fi

      - name: Download and patch
        if: steps.check.outputs.skip == 'false'
        run: |
          curl -fsSL -o upstream.ipa "${{ steps.up.outputs.url }}"
          python3 scripts/patch_ipa.py upstream.ipa "dist/$OUT_NAME"

      - name: Publish release
        if: steps.check.outputs.skip == 'false'
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh release create "${{ steps.up.outputs.tag }}" "dist/$OUT_NAME" \
            -R "$GITHUB_REPOSITORY" \
            --title "Nuvio Enhanced ${{ steps.up.outputs.tag }} (full screen)" \
            --notes "Auto-patched from ${{ steps.up.outputs.url }}" \
            --latest
