import { Routes } from '@angular/router';

export const routes: Routes = [
  {
    path: '',
    loadComponent: () => import('./features/video-list/video-list').then((m) => m.VideoList),
    title: '逐字稿審核',
  },
  {
    path: 'review/:videoId',
    loadComponent: () => import('./features/reviewer/reviewer').then((m) => m.Reviewer),
    title: '逐字稿審核器',
  },
  { path: '**', redirectTo: '' },
];
