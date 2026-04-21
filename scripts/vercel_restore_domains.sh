#!/bin/bash
# Restore all Vercel domain aliases after platform incidents
# Usage: bash scripts/vercel_restore_domains.sh

set -e
SCOPE="hector-pachaos-projects"

echo "=== Vercel Domain Restoration ==="
echo ""

# Get latest production deployment IDs
echo "Fetching latest deployments..."

HECTOR=$(npx vercel ls hector-services --scope $SCOPE --non-interactive 2>/dev/null | grep "production" | head -1 | awk '{print $2}')
PHD=$(npx vercel ls phd --scope $SCOPE --non-interactive 2>/dev/null | grep "production" | head -1 | awk '{print $2}')
SINPETCA=$(npx vercel ls sinpetca --scope $SCOPE --non-interactive 2>/dev/null | grep "production" | head -1 | awk '{print $2}')
TC=$(npx vercel ls tc-insurance --scope $SCOPE --non-interactive 2>/dev/null | grep "production" | head -1 | awk '{print $2}')

echo ""
echo "Assigning domains..."

# hector-services
npx vercel alias set "$HECTOR" pachanodesign.com --scope $SCOPE --non-interactive
echo "  pachanodesign.com -> $HECTOR"

# phd
npx vercel alias set "$PHD" premiumhome.design --scope $SCOPE --non-interactive
npx vercel alias set "$PHD" www.premiumhome.design --scope $SCOPE --non-interactive
echo "  premiumhome.design -> $PHD"

# sinpetca
npx vercel alias set "$SINPETCA" sinpetca.com --scope $SCOPE --non-interactive
npx vercel alias set "$SINPETCA" www.sinpetca.com --scope $SCOPE --non-interactive
echo "  sinpetca.com -> $SINPETCA"

# tc-insurance
npx vercel alias set "$TC" tcinsurancetx.com --scope $SCOPE --non-interactive
npx vercel alias set "$TC" www.tcinsurancetx.com --scope $SCOPE --non-interactive
echo "  tcinsurancetx.com -> $TC"

echo ""
echo "=== Verification ==="
for domain in pachanodesign.com premiumhome.design sinpetca.com www.tcinsurancetx.com; do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" "https://$domain")
  echo "  $domain -> HTTP $STATUS"
done

echo ""
echo "Done. All domains restored."
