"""Public counts for a read-only podcast configuration preview."""

def public_scope(result):
    rows=[r for r in result.get('episodes',[]) if isinstance(r,dict)]
    return dict(
        episodes=len(rows),
        position=sum(r.get('pine_mode')!='status_only' and r.get('pine_status')=='enrolled' for r in rows),
        status_only=sum(r.get('pine_mode')=='status_only' and r.get('pine_status')=='enrolled' for r in rows),
        waiting=sum(r.get('pine_status') in ('waiting','joining') or r.get('progress_status')=='waiting' for r in rows),
        blocked=sum(r.get('progress_status')=='blocked' for r in rows),
        proposed=sum(r.get('progress_status')=='proposed' for r in rows))
