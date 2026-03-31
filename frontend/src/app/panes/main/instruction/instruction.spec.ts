import { ComponentFixture, TestBed } from '@angular/core/testing';

import { Instruction } from './instruction';

describe('Instruction', () => {
  let component: Instruction;
  let fixture: ComponentFixture<Instruction>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Instruction],
    }).compileComponents();

    fixture = TestBed.createComponent(Instruction);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
