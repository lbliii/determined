import { StyleProvider } from '@ant-design/cssinjs';
import userEvent from '@testing-library/user-event';
import test from 'ava';
import React, { useEffect, useState } from 'react';
import td from 'testdouble';

import { noOp } from 'shared/utils/service';
import { hasStyle, inDocument, notInDocument, renderInContainer, waitFor } from 'test/utils';

import Spinner from './Spinner';

const spinnerTextContent = 'Spinner Text Content';

const user = userEvent.setup();
interface Props {
  handleButtonClick: td.TestDouble<() => void>;
  spinning: boolean;
}

const SpinnerComponent = ({ spinning, handleButtonClick }: Props) => {
  const [isSpin, setIsSpin] = useState<boolean>(false);

  useEffect(() => {
    setIsSpin(spinning);
  }, [spinning]);

  const onToggle = () => setIsSpin((v) => !v);

  return (
    <>
      <button data-testid="toogle-button" onClick={onToggle}>
        Toggle Spin
      </button>
      <Spinner spinning={isSpin} tip={spinnerTextContent}>
        <button data-testid="inside-button" onClick={handleButtonClick}>
          click
        </button>
      </Spinner>
    </>
  );
};

const setup = async (spinning: boolean) => {
  const handleButtonClick = td.function(noOp);
  const result = renderInContainer(
    // apply css-in-js styles without the :when selector
    <StyleProvider container={document.body} hashPriority="high">
      <SpinnerComponent handleButtonClick={handleButtonClick} spinning={spinning} />,
    </StyleProvider>,
  );
  await new Promise((resolve) => setTimeout(resolve, 10));
  return { handleButtonClick, ...result };
};

test('blocks inner content while spinning', async (t) => {
  const { container, findByTestId } = await setup(true);
  const button = await findByTestId('inside-button');
  await waitFor(t, (tt) => {
    const spin = container.querySelector('.ant-spin');
    hasStyle(tt, spin, { position: 'absolute' });
  });
  await t.throwsAsync(user.click(button));
});

test('doesnt block inner content when not spinning', async (t) => {
  const { handleButtonClick, getByTestId } = await setup(false);
  const button = getByTestId('inside-button');
  await user.click(button);
  t.is(td.explain(handleButtonClick).callCount, 1);
});

test('displays tip text when spinning', async (t) => {
  const { findByText } = await setup(true);
  inDocument(t, await findByText(spinnerTextContent));
});

test('doesnt display tip text when not spinning', async (t) => {
  const { queryByText } = await setup(false);
  notInDocument(t, queryByText(spinnerTextContent));
});

test('goes away when spinning is updated to false', async (t) => {
  const { container, getByTestId } = await setup(true);

  await waitFor(t, (tt) => {
    inDocument(tt, container.getElementsByClassName('ant-spin-spinning')[0]);
  });
  await user.click(getByTestId('toogle-button'));
  await waitFor(t, (tt) => {
    notInDocument(tt, container.getElementsByClassName('ant-spin-spinning')[0]);
  });
});

test('appears when spinning is updated to false', async (t) => {
  const { container, getByTestId } = await setup(false);
  t.falsy(container.getElementsByClassName('ant-spin-spinning')?.[0]);
  await user.click(getByTestId('toogle-button'));
  await waitFor(t, (tt) => {
    inDocument(tt, container.getElementsByClassName('ant-spin-spinning')[0]);
  });
});
