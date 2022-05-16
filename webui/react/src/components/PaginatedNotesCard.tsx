import { CheckOutlined } from '@ant-design/icons';
import { Button, Dropdown, Menu, Modal, Select } from 'antd';
import { SelectValue } from 'antd/lib/select';
import React, { useCallback, useEffect, useState } from 'react';

import usePrevious from 'hooks/usePrevious';
import { Note } from 'types';

import Icon from './Icon';
import NotesCard from './NotesCard';
import css from './PaginatedNotesCard.module.scss';
import SelectFilter from './SelectFilter';

const { Option } = Select;

interface Props {
  disabled?: boolean;
  notes: Note[];
  onDelete: (pageNumber: number) => void;
  onNewPage: () => void;
  onSave: (notes: Note[]) => void;
}

const PaginatedNotesCard: React.FC<Props> = (
  { notes, onNewPage, onSave, onDelete, disabled = false }:Props,
) => {
  const [ currentPage, setCurrentPage ] = useState(0);
  const [ deleteTarget, setDeleteTarget ] = useState(0);
  const [ editedContents, setEditedContents ] = useState(notes?.[currentPage]?.contents ?? '');
  const [ editedName, setEditedName ] = useState(notes?.[currentPage]?.name ?? '');
  const [ modal, contextHolder ] = Modal.useModal();
  const [ noteChangeSignal, setNoteChangeSignal ] = useState(1);
  const fireNoteChangeSignal =
  useCallback(
    () => setNoteChangeSignal((prev) => (prev === 100 ? 1 : prev + 1)),
    [ setNoteChangeSignal ],
  );

  const previousNumberOfNotes = usePrevious(notes.length, undefined);

  const handleSwitchPage = useCallback((pageNumber: number | SelectValue) => {
    if (pageNumber === currentPage) return;
    if (editedContents !== notes?.[currentPage]?.contents) {
      modal.confirm({
        content: (
          <p>
            You have unsaved notes, are you sure you want to switch pages?
            Unsaved notes will be lost.
          </p>),
        onOk: () => { setCurrentPage(pageNumber as number); fireNoteChangeSignal(); },
        title: 'Unsaved content',
      });
    } else {
      setCurrentPage(pageNumber as number);
      setEditedContents(notes?.[currentPage]?.contents ?? '');
      fireNoteChangeSignal();
    }
  }, [ currentPage, editedContents, modal, notes, fireNoteChangeSignal ]);

  useEffect(() => {
    if (!previousNumberOfNotes || notes.length > previousNumberOfNotes) {
      handleSwitchPage(notes.length);
    } else if (notes.length < previousNumberOfNotes){
      // dont call handler here because page isn't actually switching
      setCurrentPage((prevPageNumber) =>
        prevPageNumber > deleteTarget ? prevPageNumber - 1 : prevPageNumber);
    }
  }, [ previousNumberOfNotes, notes.length, deleteTarget, handleSwitchPage ]);

  const handleNewPage = useCallback(() => {
    const currentPages = notes.length;
    onNewPage();
    handleSwitchPage(currentPages);
  }, [ notes.length, onNewPage, handleSwitchPage ]);

  const handleSave = useCallback(async (editedNotes: string) => {
    setEditedContents(editedNotes);
    await onSave(notes.map((note, idx) => {
      if (idx === currentPage) {
        return { contents: editedNotes, name: editedName } as Note;
      }
      return note;
    }));
  }, [ currentPage, editedName, notes, onSave ]);

  const handleSaveTitle = useCallback(async (newName: string) => {
    setEditedName(newName);
    await onSave(notes.map((note, idx) => {
      if (idx === currentPage) {
        return { ...note, name: newName } as Note;
      }
      return note;
    }));
  }, [ currentPage, notes, onSave ]);

  const handleDeletePage = useCallback((deletePageNumber: number) => {
    onDelete(deletePageNumber);
    setDeleteTarget(deletePageNumber);
  }, [ onDelete, setDeleteTarget ]);

  const handleEditedNotes = useCallback((newContents: string) => {
    setEditedContents(newContents);
  }, []);

  useEffect(() => {
    if (notes.length === 0) return;
    if (currentPage < 0) { setCurrentPage(0); fireNoteChangeSignal(); }
    if (currentPage >= notes.length) { setCurrentPage(notes.length - 1); fireNoteChangeSignal(); }
  }, [ currentPage, notes.length, fireNoteChangeSignal ]);

  useEffect(() => {
    setEditedContents(notes?.[currentPage]?.contents ?? '');
    setEditedName(notes?.[currentPage]?.name ?? '');
  }, [ currentPage, notes ]);

  const ActionMenu = useCallback((pageNumber: number) => {
    return (
      <Menu onClick={({ domEvent }) => domEvent.stopPropagation()}>
        <Menu.Item
          danger
          key="delete"
          onClick={() => handleDeletePage(pageNumber)}>
          Delete...
        </Menu.Item>
      </Menu>
    );
  }, [ handleDeletePage ]);

  if (notes.length === 0) {
    return (
      <div className={css.emptyBase}>
        <div className={css.messageContainer}>
          <Icon name="document" size="mega" />
          <p>No notes for this project</p>
          <Button onClick={handleNewPage}>+ New Page</Button>
        </div>
      </div>
    );
  }

  return (
    <div className={css.base}>
      {notes.length > 0 && (
        <div className={css.sidebar}>
          <ul className={css.listContainer} role="list">
            {(notes as Note[]).map((note, idx) => (
              <Dropdown
                disabled={disabled}
                key={idx}
                overlay={() => ActionMenu(idx)}
                trigger={[ 'contextMenu' ]}>
                <li
                  className={css.listItem}
                  style={{
                    borderColor: idx === currentPage ?
                      'var(--theme-colors-monochrome-12)' :
                      undefined,
                  }}
                  onClick={() => handleSwitchPage(idx)}>
                  <span>{note.name}</span>
                  {!disabled && (
                    <Dropdown
                      overlay={() => ActionMenu(idx)}
                      trigger={[ 'click' ]}>
                      <div className={css.action} onClick={e => e.stopPropagation()}>
                        <Icon name="overflow-horizontal" />
                      </div>
                    </Dropdown>
                  )}
                </li>
              </Dropdown>
            ))}
          </ul>
        </div>
      )}
      <div className={css.pageSelectRow}>
        <SelectFilter
          className={css.pageSelect}
          size="large"
          value={currentPage}
          onSelect={handleSwitchPage}>
          {notes.map((note, idx) => {
            return (
              <Option className={css.selectOption} key={idx} value={idx}>
                <CheckOutlined
                  className={css.currentPage}
                  style={{
                    marginRight: 'var(--theme-sizes-layout-small)',
                    visibility: idx === currentPage ? 'visible' : 'hidden',
                  }}
                />
                <span>{note.name}</span>
              </Option>
            );
          })}
        </SelectFilter>
      </div>
      <div className={css.notesContainer}>
        <NotesCard
          disabled={disabled}
          extra={(
            <Dropdown
              overlay={() => ActionMenu(currentPage)}
              trigger={[ 'click' ]}>
              <div style={{ cursor: 'pointer' }}>
                <Icon name="overflow-horizontal" />
              </div>
            </Dropdown>
          )}
          noteChangeSignal={noteChangeSignal}
          notes={notes?.[currentPage]?.contents ?? ''}
          style={{ border: 0 }}
          title={notes?.[currentPage]?.name ?? ''}
          onChange={handleEditedNotes}
          onSave={handleSave}
          onSaveTitle={handleSaveTitle}
        />
      </div>
      {contextHolder}
    </div>
  );
};

export default PaginatedNotesCard;
