# Sourced. The storage entry of packer-manifest.json, by name as the k3s steps read theirs. Plain assignments, then
# export: `export X=$(...)` would hide jq's exit status from sh -e. No match makes `last` null, so jq -e fails.
entry=$(jq -ec '[.builds[] | select(.name=="storage")] | last' packer-manifest.json)
TF_VAR_storage_template_vmid=$(printf '%s' "$entry" | jq -er '.artifact_id | tonumber | select(. == floor and . > 0)')
TF_VAR_storage_template_name=$(printf '%s' "$entry" | jq -er '.custom_data.vm_name')
export TF_VAR_storage_template_vmid TF_VAR_storage_template_name
unset entry
