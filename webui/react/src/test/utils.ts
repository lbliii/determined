import { waitFor as baseWaitFor } from '@testing-library/react';
import type { ExecutionContext, Implementation, TryResult } from 'ava';

const getStyleDeclaration = (css: Record<string, string>) => {
  const finalStyle: Record<string, string> = {};

  // normalize colors
  const div = document.createElement('div');
  Object.entries(css).forEach(([key, value]) => {
    // css indexer behavior doesn't mesh well with typescript
    div.style[key as any] = value;
    finalStyle[key] = div.style[key as any];
  });

  return finalStyle;
};

// small implementation of toHaveStyle assertion matcher from @testing-library/jest-dom -- doesn't support css parsing
export const hasStyle = (
  t: ExecutionContext,
  element: Element | null,
  style: Record<string, string>,
): boolean => {
  t.not(element, null);
  // above assertion filters out null
  const actualStyle = window.getComputedStyle(element as Element);
  const expectedStyle = getStyleDeclaration(style);

  return t.like(actualStyle, expectedStyle);
};

export const inDocument = (t: ExecutionContext, element: Element | null): boolean => {
  t.not(element, null);
  // above assertion filters out null
  const e = element as Element;
  return t.is(e.ownerDocument as Node, e.getRootNode({ composed: true }));
};

export const notInDocument = (t: ExecutionContext, element: Element | null): boolean => {
  return t.falsy(element) || t.falsy(inDocument(t, element));
};

// convenience wrapper around testing-library#waitFor that leverages try
export const waitFor = (t: ExecutionContext, fn: Implementation<[]>): Promise<TryResult> => {
  return baseWaitFor(async () => {
    const retVal = await t.try(fn);
    if (retVal.passed) {
      retVal.commit();
      return retVal;
    }
    retVal.discard();
    throw retVal;
  }, {});
};
