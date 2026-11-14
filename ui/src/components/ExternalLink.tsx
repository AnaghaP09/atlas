import { useState } from "react";
import { safeExternalUrl } from "../lib/links";

/**
 * A link that leaves ATLAS. Only https links to listed hosts are shown as links; the query string is
 * removed, no referrer is sent, and the user confirms before leaving. Anything else is shown as
 * plain text.
 */
export function ExternalLink({ href, label }: { href: string; label: string }) {
  const [asking, setAsking] = useState(false);
  const safe = safeExternalUrl(href);
  if (!safe) return <span className="link-blocked" title="This link is not on the allowed list.">{label}</span>;
  if (!asking)
    return (
      <button type="button" className="link-button" onClick={() => setAsking(true)}>
        {label}
      </button>
    );
  return (
    <span className="interstitial" role="dialog" aria-label="Leave ATLAS">
      You are leaving ATLAS for <strong>{safe.host}</strong>.{" "}
      <a href={safe.href} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" onClick={() => setAsking(false)}>
        Open {safe.href}
      </a>{" "}
      <button type="button" onClick={() => setAsking(false)}>
        Stay
      </button>
    </span>
  );
}
