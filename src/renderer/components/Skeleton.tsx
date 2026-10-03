import React from 'react';

const Skeleton: React.FC<{ className?: string }> = ({ className = '' }) => (
  <span aria-hidden="true" className={`block rounded bg-surface-700 motion-safe:animate-pulse ${className}`} />
);

export default Skeleton;
