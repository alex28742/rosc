import type { SVGProps } from 'react';

/**
 * Decorative inline icons shared by the package. They inherit `currentColor` and never carry
 * meaning on their own: every icon sits next to a text label or inside an element that already
 * has an accessible name.
 */
type IconProps = SVGProps<SVGSVGElement> & { size?: 'sm' | 'md' };

function Frame({ size = 'md', children, ...rest }: IconProps & { children: React.ReactNode }) {
  return <svg className={size === 'sm' ? 'rosc-icon rosc-icon--sm' : 'rosc-icon'} viewBox="0 0 24 24" aria-hidden="true" focusable="false" {...rest}>{children}</svg>;
}

export const CloseIcon = (props: IconProps) => <Frame {...props}><path d="M6 6l12 12M18 6L6 18" /></Frame>;

export const ChevronRightIcon = (props: IconProps) => <Frame {...props}><path d="M9 5l7 7-7 7" /></Frame>;

export const ArrowUpRightIcon = (props: IconProps) => <Frame {...props}><path d="M7 17L17 7M9 7h8v8" /></Frame>;

export const DocumentIcon = (props: IconProps) => <Frame {...props}><path d="M14 3H7a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V7z" /><path d="M14 3v4h4M9 12h6M9 16h6" /></Frame>;

export const FolderIcon = (props: IconProps) => <Frame {...props}><path d="M3 7a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z" /></Frame>;

export const AlertIcon = (props: IconProps) => <Frame {...props}><circle cx="12" cy="12" r="9" /><path d="M12 7v6M12 16.5v.5" /></Frame>;

export const CheckIcon = (props: IconProps) => <Frame {...props}><path d="M5 12.5l4.5 4.5L19 7" /></Frame>;
