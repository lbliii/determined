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

const isNotNull = <T>(t: ExecutionContext, e: T): e is Exclude<T, null | undefined> => t.truthy(e);

// small implementation of toHaveStyle assertion matcher from @testing-library/jest-dom -- doesn't support css parsing
export const hasStyle = (
  t: ExecutionContext,
  element: Element | null,
  style: Record<string, string>,
): boolean => {
  if (isNotNull(t, element)) {
    const actualStyle = window.getComputedStyle(element as Element);
    const expectedStyle = getStyleDeclaration(style);

    return t.like(actualStyle, expectedStyle);
  }
  return false;
};

export const inDocument = (t: ExecutionContext, element: Element | null): boolean => {
  return (
    isNotNull(t, element) &&
    t.is(element.ownerDocument as Node, element.getRootNode({ composed: true }))
  );
};

export const notInDocument = (t: ExecutionContext, element: Element | null): boolean => {
  return t.falsy(element) || t.false(inDocument(t, element));
};

export const hasClass = (t: ExecutionContext, element: Element | null, ...classes: string[]) => {
  return isNotNull(t, element) && isSubsetOfArray(t, [...element.classList], classes);
};

export const isSubsetOfArray = async (
  t: ExecutionContext,
  value: any[],
  selector: any[],
): Promise<boolean> => {
  const sliceSize = selector.length;
  const maxIndex = value.length;
  let lastTry;
  for (let i = 0; i + sliceSize <= maxIndex; i++) {
    lastTry = await t.try((tt) => tt.deepEqual(value.slice(i, i + sliceSize), selector));
    if (lastTry.passed || i + 1 + sliceSize > maxIndex) {
      lastTry.commit();
      break;
    }
    lastTry.discard();
  }
  return !!lastTry?.passed;
};

type AllButFirstParam<T> = T extends (arg: any, ...args: infer R) => any ? R : never;
// convenience wrapper around testing-library#waitFor that leverages try
export const waitFor = (
  t: ExecutionContext,
  fn: Implementation<[]>,
  ...args: AllButFirstParam<typeof baseWaitFor>
): Promise<TryResult> => {
  return baseWaitFor(async () => {
    const retVal = await t.try(fn);
    if (retVal.passed) {
      retVal.commit();
      return retVal;
    }
    retVal.discard();
    throw retVal;
  }, ...args);
};
