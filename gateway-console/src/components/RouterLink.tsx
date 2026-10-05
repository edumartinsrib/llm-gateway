import { forwardRef } from 'react';
import { Link, type LinkProps } from 'react-router';

/** Adaptador do link da broto-ui (`href`) para o react-router (`to`). */
export const RouterLink = forwardRef<HTMLAnchorElement, Omit<LinkProps, 'to'> & { href: string }>(
  ({ href, ...rest }, ref) => <Link ref={ref} to={href} {...rest} />,
);
RouterLink.displayName = 'RouterLink';
