import { Routes } from '@angular/router';

export const routes: Routes = [
  {
    path: '',
    loadComponent: () => import('./features/video-list/video-list').then((m) => m.VideoList),
    title: '影片總覽',
  },
  {
    path: 'pipeline/:videoId',
    loadComponent: () =>
      import('./features/pipeline/pipeline-detail').then((m) => m.PipelineDetail),
    title: 'Pipeline 進度',
  },
  {
    path: 'review/:videoId',
    loadComponent: () => import('./features/reviewer/reviewer').then((m) => m.Reviewer),
    title: '逐字稿審核器',
  },
  {
    path: 'summary/:videoId',
    loadComponent: () => import('./features/summary/summary').then((m) => m.Summary),
    title: '摘要審核',
  },
  {
    path: 'publish/:videoId',
    loadComponent: () => import('./features/publish/publish').then((m) => m.Publish),
    title: '圖卡與發布',
  },
  {
    path: 'sources',
    loadComponent: () => import('./features/sources/sources').then((m) => m.Sources),
    title: '訂閱來源管理',
  },
  { path: '**', redirectTo: '' },
];
