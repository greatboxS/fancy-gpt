/* Carry a watched site's shapes back, on a site we do not drive.
 *
 * The page hook watches several sites, but only the ones with an adapter have
 * a content script listening for what it reports. On the rest its messages had
 * nowhere to go, which made granting observation pointless: the whole purpose
 * of watching an unmeasured site is to find out how it answers before writing
 * anything for it.
 *
 * There is no new channel here. Reports go to the background worker, which
 * keeps a bounded few and attaches them to the next turn that runs on a site
 * we do drive -- so a shape measured on Grok arrives in the diagnostics of an
 * ordinary ChatGPT turn, through plumbing that already works.
 */
const ext = globalThis.browser ?? globalThis.chrome;

window.addEventListener("message", event => {
  if (event.source !== window) return;
  const data = event.data;
  if (!data || data.source !== "fancygpt-page-hook") return;
  const {source, ...report} = data;
  // Shapes and counts only, which is all the hook reports for a site that has
  // no decoder: nothing is forwarded from a transport nobody has looked at.
  if (report.kind === "events") return;
  try {
    ext.runtime.sendMessage({
      type: "fancy_site_observation",
      origin: location.origin,
      report,
    }).catch(() => {});
  } catch (_) {}
});
