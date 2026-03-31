import { ComponentFixture, TestBed } from '@angular/core/testing';

import { UseExistingBmrb } from './use-existing-bmrb';

describe('UseExistingBmrb', () => {
  let component: UseExistingBmrb;
  let fixture: ComponentFixture<UseExistingBmrb>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [UseExistingBmrb],
    }).compileComponents();

    fixture = TestBed.createComponent(UseExistingBmrb);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
