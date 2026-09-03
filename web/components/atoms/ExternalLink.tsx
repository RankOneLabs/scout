"use client";

import { ExternalLink as ExternalLinkIcon } from "lucide-react";

interface ExternalLinkProps {
  href: string;
  children: React.ReactNode;
}

export function ExternalLink({ href, children }: ExternalLinkProps) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1 text-sm text-blue-600 transition-colors hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
    >
      {children}
      <ExternalLinkIcon className="h-3 w-3" />
    </a>
  );
}
