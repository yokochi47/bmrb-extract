import { ComponentFixture, TestBed } from '@angular/core/testing';

import { ProcessLog } from './process-log';

describe('ProcessLog', () => {
  let component: ProcessLog;
  let fixture: ComponentFixture<ProcessLog>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ProcessLog],
    }).compileComponents();

    fixture = TestBed.createComponent(ProcessLog);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
