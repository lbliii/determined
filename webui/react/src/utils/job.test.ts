import test from 'ava';

import * as Api from 'services/api-ts-sdk';
import { CommandType, Job, JobType } from 'types';

import * as utils from './job';

test('jobTypeIconName should support experiment and command types', (t) => {
  t.is(utils.jobTypeIconName(JobType.EXPERIMENT), 'experiment');
  t.is(utils.jobTypeIconName(JobType.NOTEBOOK), CommandType.JupyterLab);
});

test('jobTypeToCommandType should convert notebook to jupyterlab', (t) => {
  t.is(utils.jobTypeToCommandType(JobType.NOTEBOOK), CommandType.JupyterLab);
});
test('jobTypeToCommandType should return undefined for non command types', (t) => {
  t.is(utils.jobTypeToCommandType(JobType.EXPERIMENT), undefined);
});

const jobId = 'jobId1';
const jobs = [
  { jobId: 'jobId1', summary: { jobsAhead: 0 } },
  { jobId: 'jobId2', summary: { jobsAhead: 1 } },
  { jobId: 'jobId3', summary: { jobsAhead: 2 } },
  { jobId: 'jobId4', summary: { jobsAhead: 3 } },
] as Job[];

// TODO more tests
test('moveJobToPositionUpdate should avoid updating if the position is the same', (t) => {
  const position = 1;
  t.is(utils.moveJobToPositionUpdate(jobs, jobId, position), undefined);
});

test('moveJobToPositionUpdate should use behindOf for putting the job last', (t) => {
  const expected: Api.V1QueueControl = {
    behindOf: jobs.last().jobId,
    jobId,
  };
  t.deepEqual(utils.moveJobToPositionUpdate(jobs, jobId, jobs.length), expected);
});

test('moveJobToPositionUpdate should throw given invalid position input', (t) => {
  const invalid1 = t.throws(() => utils.moveJobToPositionUpdate(jobs, jobId, -1));
  t.is(invalid1?.message, 'Moving job failed.');
  const invalid2 = t.throws(() => utils.moveJobToPositionUpdate(jobs, jobId, 0.3));
  t.is(invalid2?.message, 'Moving job failed.');
});

test('moveJobToPositionUpdate should work on middle of the job queue for moving up', (t) => {
  const id = 'jobId3';
  const expected: Api.V1QueueControl = {
    aheadOf: 'jobId2',
    jobId: id,
  };
  t.deepEqual(utils.moveJobToPositionUpdate(jobs, id, 2), expected);
});

test('moveJobToPositionUpdate should work on middle of the job queue for moving down', (t) => {
  const id = 'jobId2';
  const expected: Api.V1QueueControl = {
    behindOf: 'jobId3',
    jobId: id,
  };
  t.deepEqual(utils.moveJobToPositionUpdate(jobs, id, 3), expected);
});
