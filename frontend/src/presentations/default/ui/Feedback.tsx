import { useEffect, useRef } from 'react';
import type { Feedback as FeedbackProps } from '../../../app/presentation/contracts';

/** Error/notice for the active layer. A new message is scrolled into view so a failed submit is never off-screen. */
export function Feedback({ error, notice }: FeedbackProps) {
  const errorRef = useRef<HTMLDivElement>(null);
  const noticeRef = useRef<HTMLDivElement>(null);
  useEffect(() => { if (error) errorRef.current?.scrollIntoView?.({ block: 'nearest' }); }, [error]);
  useEffect(() => { if (notice) noticeRef.current?.scrollIntoView?.({ block: 'nearest' }); }, [notice]);
  return <>
    {error && <div ref={errorRef} role="alert" className="rosc-alert">{error}</div>}
    {notice && <div ref={noticeRef} role="status" className="rosc-notice">{notice}</div>}
  </>;
}
