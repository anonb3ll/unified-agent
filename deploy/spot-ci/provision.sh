#!/usr/bin/env bash
# Create the Spot CI runner for scripts/cloud-ci.sh (task #2252). Idempotent.
#
#   UA_CI_GCP_PROJECT=<project> deploy/spot-ci/provision.sh
#
# - bucket <project>-spot-ci: run sources, state and logs; deleted after 30 days
# - service account ua-ci-runner: object admin on that bucket only
# - VM ua-ci-runner: Spot e2-highcpu-8 (the Cloud Build machine type: 8 vCPU,
#   8 GB), 30 GB pd-balanced, no external IP (egress through the project's
#   Cloud NAT, which must already exist: this script checks and never creates
#   it), stopped by GCE after 3 h of running whatever the script does.
#   It is created stopped; scripts/spot_ci.py starts it per run and the
#   startup script powers it off when the queue is empty.
set -euo pipefail
project="${UA_CI_GCP_PROJECT:?set UA_CI_GCP_PROJECT}"
zone="${UA_CI_SPOT_ZONE:-us-central1-a}"
instance="${UA_CI_SPOT_INSTANCE:-ua-ci-runner}"
bucket="${UA_CI_SPOT_BUCKET:-$project-spot-ci}"
sa="ua-ci-runner@$project.iam.gserviceaccount.com"
here="$(cd "$(dirname "$0")" && pwd)"
region="${zone%-*}"

# The VM has no external IP, so without Cloud NAT in its region it cannot pull
# packages or images and every run would fail on the VM. Fail before changing
# anything; creating a NAT is a network decision for the project owner. The VM
# is created on the default network, so only a router on that network counts.
has_nat=""
for router in $(gcloud compute routers list --project="$project" \
    --filter="region:$region AND network~/networks/default$" --format='value(name)'); do
    if [ -n "$(gcloud compute routers nats list --router="$router" --region="$region" \
        --project="$project" --format='value(name)')" ]; then
        has_nat=1
        break
    fi
done
if [ -z "$has_nat" ]; then
    echo "provision.sh: no Cloud NAT gateway on the default network in $project/$region." >&2
    echo "The runner VM has no external IP and needs Cloud NAT to reach the internet." >&2
    echo "Create a Cloud Router and NAT for the default network in $region (gcloud compute routers create/nats create)," >&2
    echo "then rerun. Nothing was created." >&2
    exit 1
fi

if ! gcloud storage buckets describe "gs://$bucket" --project="$project" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://$bucket" --project="$project" --location="$region" \
        --uniform-bucket-level-access --public-access-prevention
fi
lifecycle="$(mktemp)"
printf '{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 30}}]}\n' > "$lifecycle"
gcloud storage buckets update "gs://$bucket" --lifecycle-file="$lifecycle" >/dev/null
rm -f "$lifecycle"

if ! gcloud iam service-accounts describe "$sa" --project="$project" >/dev/null 2>&1; then
    gcloud iam service-accounts create ua-ci-runner --project="$project" \
        --display-name="Spot CI runner (task #2252)"
fi
gcloud storage buckets add-iam-policy-binding "gs://$bucket" \
    --member="serviceAccount:$sa" --role=roles/storage.objectAdmin >/dev/null

if ! gcloud compute instances describe "$instance" --project="$project" --zone="$zone" >/dev/null 2>&1; then
    gcloud compute instances create "$instance" --project="$project" --zone="$zone" \
        --machine-type=e2-highcpu-8 \
        --provisioning-model=SPOT --instance-termination-action=STOP --max-run-duration=3h \
        --image-family=debian-12 --image-project=debian-cloud \
        --boot-disk-size=30GB --boot-disk-type=pd-balanced \
        --no-address --service-account="$sa" --scopes=cloud-platform \
        --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
        --metadata="ua-ci-bucket=$bucket,enable-oslogin=TRUE" \
        --metadata-from-file="startup-script=$here/startup.sh" \
        --labels=purpose=ci,task=2252
fi
echo "Spot CI runner ready: $instance in $project/$zone, bucket gs://$bucket"
