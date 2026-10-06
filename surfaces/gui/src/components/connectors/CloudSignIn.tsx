// Fork note: the upstream "OpenWorker Cloud" sign-in (Google/GitHub OAuth, managed
// one-click connectors) is NOT used by this fork. Connectors connect via Manual tokens
// instead, so this pane just points at that — there is no official-cloud login button.
export function CloudSignInInline({ blurb }: { blurb?: string }) {
  return (
    <div className="space-y-1.5">
      <div className="text-[12px] text-faint">
        {blurb || "使用「手动连接」填入令牌即可，无需登录官方 OpenWorker Cloud。"}
      </div>
    </div>
  );
}

// The UNKNOWN state: the status fetch hasn't resolved (or is being retried).
export function CloudStatusPending() {
  return (
    <div
      className="text-[12px] text-faint py-2 text-center"
      data-testid="cloud-status-pending"
    >
      Checking connection status…
    </div>
  );
}
