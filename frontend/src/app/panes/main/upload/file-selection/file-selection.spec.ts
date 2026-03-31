import { ComponentFixture, TestBed } from '@angular/core/testing';

import { FileSelection } from './file-selection';

describe('FileSelection', () => {
  let component: FileSelection;
  let fixture: ComponentFixture<FileSelection>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [FileSelection],
    }).compileComponents();

    fixture = TestBed.createComponent(FileSelection);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
